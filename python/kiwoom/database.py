import sqlite3
import json
import os
import time
from datetime import datetime

DB_PATH = "/data/kiwoom_bot.db"

class BotDB:
    def __init__(self):
        self._init_db()

    def _get_conn(self):
        return sqlite3.connect(DB_PATH, check_same_thread=False)

    def _init_db(self):
        with self._get_conn() as conn:
            c = conn.cursor()
            # 1. 키-값 저장소
            c.execute('''CREATE TABLE IF NOT EXISTS kv_store (
                        key TEXT PRIMARY KEY,
                        value TEXT,
                        updated_at TEXT
                    )''')
            
            # 2. 매매 로그
            c.execute('''CREATE TABLE IF NOT EXISTS trade_logs (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        timestamp TEXT,
                        action TEXT,
                        stock_code TEXT,
                        stock_name TEXT,
                        qty INTEGER,
                        price REAL,
                        reason TEXT,
                        profit_rate REAL,
                        profit_amt INTEGER,
                        image_path TEXT,
                        ai_reason TEXT
                    )''')

            # 3. 명령 큐
            c.execute('''CREATE TABLE IF NOT EXISTS command_queue (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        cmd_type TEXT,
                        payload TEXT,
                        status TEXT DEFAULT 'PENDING',
                        created_at TEXT
                    )''')
            
            # 🌟 [신규] 시스템 로그 테이블
            c.execute('''CREATE TABLE IF NOT EXISTS system_logs (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        timestamp TEXT,
                        level TEXT,
                        module TEXT,
                        message TEXT
                    )''')
            conn.commit()

    # --- KV Store 메서드 ---
    def get_kv(self, key, default=None):
        try:
            with self._get_conn() as conn:
                c = conn.cursor()
                c.execute("SELECT value FROM kv_store WHERE key=?", (key,))
                row = c.fetchone()
                if row:
                    try: return json.loads(row[0])
                    except: return row[0]
                return default
        except: return default

    def set_kv(self, key, value):
        try:
            with self._get_conn() as conn:
                c = conn.cursor()
                val_str = json.dumps(value, ensure_ascii=False)
                now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                c.execute("INSERT OR REPLACE INTO kv_store (key, value, updated_at) VALUES (?, ?, ?)", 
                          (key, val_str, now))
                conn.commit()
        except: pass

    # --- Trade Log 메서드 ---
    def log_trade(self, data):
        try:
            with self._get_conn() as conn:
                c = conn.cursor()
                c.execute('''INSERT INTO trade_logs 
                            (timestamp, action, stock_code, stock_name, qty, price, reason, profit_rate, profit_amt, image_path, ai_reason)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                          (data['timestamp'], data['action'], data['stock_code'], data['stock_name'], 
                           data['qty'], data['price'], data['reason'], data['profit_rate'], 
                           data['profit_amt'], data.get('image_path'), data.get('ai_reason')))
                conn.commit()
        except: pass

    def get_recent_trades(self, limit=100):
        try:
            with self._get_conn() as conn:
                conn.row_factory = sqlite3.Row
                c = conn.cursor()
                c.execute("SELECT * FROM trade_logs ORDER BY id DESC LIMIT ?", (limit,))
                return [dict(row) for row in c.fetchall()]
        except: return []

    # --- Command 메서드 ---
    def pop_command(self):
        try:
            with self._get_conn() as conn:
                conn.row_factory = sqlite3.Row
                c = conn.cursor()
                c.execute("SELECT * FROM command_queue WHERE status='PENDING' ORDER BY id ASC LIMIT 1")
                row = c.fetchone()
                if row:
                    c.execute("UPDATE command_queue SET status='DONE' WHERE id=?", (row['id'],))
                    conn.commit()
                    return dict(row)
                return None
        except: return None

    # 🌟 [신규] 시스템 로그 저장 메서드
    def save_system_log(self, level, message, module="Bot"):
        try:
            with self._get_conn() as conn:
                c = conn.cursor()
                now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                c.execute("INSERT INTO system_logs (timestamp, level, module, message) VALUES (?, ?, ?, ?)", 
                          (now, level, module, str(message)))
                conn.commit()
        except: pass

db = BotDB()