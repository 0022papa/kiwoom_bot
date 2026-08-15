import sqlite3
import json
import os
import time
from datetime import datetime, timedelta
from contextlib import closing # 🌟 [추가] 연결 자동 닫기를 위해 필요
import logging
import threading

DB_PATH = "/data/kiwoom_bot.db"

class BotDB:
    def __init__(self):
        self._init_db()
        self.lock = threading.Lock()

    def _get_conn(self):
        # timeout 설정 유지
        conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30.0)
        conn.execute("PRAGMA journal_mode=WAL;")
        return conn

    def _init_db(self):
        # 🌟 closing을 사용하여 블록 종료 시 자동으로 close() 호출
        with closing(self._get_conn()) as conn:
            with conn: # 트랜잭션 처리 (commit/rollback)
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
                
                # 4. 시스템 로그
                c.execute('''CREATE TABLE IF NOT EXISTS system_logs (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            timestamp TEXT,
                            level TEXT,
                            module TEXT,
                            message TEXT
                        )''')

    # --- KV Store 메서드 ---
    def get_kv(self, key, default=None):
        try:
            # 읽기 작업은 동시 허용 가능하나, 안전을 위해 락을 걸거나 짧게 유지
            with closing(self._get_conn()) as conn:
                c = conn.cursor()
                c.execute("SELECT value FROM kv_store WHERE key=?", (key,))
                row = c.fetchone()
                if row:
                    try: return json.loads(row[0])
                    except: return row[0]
                return default
        except Exception as e:
            logging.error(f"❌ [DB] get_kv({key}) 읽기 실패: {e}")
            return default

    def set_kv(self, key, value):
        try:
            with self.lock:
                with closing(self._get_conn()) as conn:
                    with conn: # 커밋 자동 처리
                        c = conn.cursor()
                        val_str = json.dumps(value, ensure_ascii=False)
                        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                        c.execute("INSERT OR REPLACE INTO kv_store (key, value, updated_at) VALUES (?, ?, ?)", 
                                  (key, val_str, now))
        except Exception as e:
            logging.error(f"❌ [DB] set_kv({key}) 저장 실패: {e}")

    # --- Trade Log 메서드 ---
    def log_trade(self, data):
        try:
            with self.lock:
                with closing(self._get_conn()) as conn:
                    with conn:
                        c = conn.cursor()
                        c.execute('''INSERT INTO trade_logs 
                                    (timestamp, action, stock_code, stock_name, qty, price, reason, profit_rate, profit_amt, image_path, ai_reason)
                                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                                  (data['timestamp'], data['action'], data['stock_code'], data['stock_name'], 
                                   data['qty'], data['price'], data['reason'], data['profit_rate'], 
                                   data['profit_amt'], data.get('image_path'), data.get('ai_reason')))
        except: pass

    def get_recent_trades(self, limit=100):
        try:
            with closing(self._get_conn()) as conn:
                conn.row_factory = sqlite3.Row
                c = conn.cursor()
                c.execute("SELECT * FROM trade_logs ORDER BY id DESC LIMIT ?", (limit,))
                return [dict(row) for row in c.fetchall()]
        except: return []

    # --- Command 메서드 ---
    def pop_command(self):
        try:
            with self.lock:
                with closing(self._get_conn()) as conn:
                    conn.row_factory = sqlite3.Row
                    c = conn.cursor()
                    # 트랜잭션 시작 (조회 후 업데이트까지 원자성 보장 권장되나, 여기선 간단히 처리)
                    c.execute("BEGIN IMMEDIATE") 
                    c.execute("SELECT * FROM command_queue WHERE status='PENDING' ORDER BY id ASC LIMIT 1")
                    row = c.fetchone()
                    if row:
                        c.execute("UPDATE command_queue SET status='DONE' WHERE id=?", (row['id'],))
                        conn.commit()
                        return dict(row)
                    conn.commit() # 조회만 했더라도 커밋/롤백으로 트랜잭션 종료
                    return None
        except: return None

    # --- 시스템 로그 ---
    def save_system_log(self, level, message, module="Bot"):
        try:
            with self.lock:
                with closing(self._get_conn()) as conn:
                    with conn:
                        c = conn.cursor()
                        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                        c.execute("INSERT INTO system_logs (timestamp, level, module, message) VALUES (?, ?, ?, ?)", 
                                  (now, level, module, str(message)))
        except: pass

    # --- 데이터 정리 ---
    def cleanup_old_data(self, days=7):
        try:
            cutoff_date = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d %H:%M:%S')
            with self.lock:
                with closing(self._get_conn()) as conn:
                    with conn:
                        c = conn.cursor()
                        c.execute("DELETE FROM trade_logs WHERE timestamp < ?", (cutoff_date,))
                        trade_count = c.rowcount
                        c.execute("DELETE FROM system_logs WHERE timestamp < ?", (cutoff_date,))
                        log_count = c.rowcount
                        c.execute("DELETE FROM command_queue WHERE status='DONE' AND created_at < ?", (cutoff_date,))
                        return trade_count, log_count
        except Exception as e:
            return 0, 0

db = BotDB()