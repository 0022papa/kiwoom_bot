import os
import json
import time
import sqlite3
from dotenv import load_dotenv

# ---------------------------------------------------------
# 1. 환경 변수 로드 (.env 파일)
# ---------------------------------------------------------
load_dotenv() 

def str_to_bool(val):
    if val is None: return False
    return str(val).lower() in ('true', '1', 't', 'yes', 'on')

# ---------------------------------------------------------
# 2. 파일 및 DB 경로 설정
# ---------------------------------------------------------
DATA_DIR = "/data"
DB_PATH = os.path.join(DATA_DIR, "kiwoom_bot.db")

# ---------------------------------------------------------
# 3. 설정 로드 (우선순위: DB > .env)
# ---------------------------------------------------------
# 기본값
MOCK_TRADE = str_to_bool(os.getenv("MOCK_TRADE", "True"))
DEBUG_MODE = str_to_bool(os.getenv("DEBUG_MODE", "False"))

# DB에서 모의투자 여부 확인
if os.path.exists(DB_PATH):
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT value FROM kv_store WHERE key='settings'")
        row = cursor.fetchone()
        if row:
            settings = json.loads(row[0])
            if "MOCK_TRADE" in settings:
                MOCK_TRADE = str_to_bool(settings["MOCK_TRADE"])
            if "DEBUG_MODE" in settings:
                DEBUG_MODE = str_to_bool(settings["DEBUG_MODE"])
        conn.close()
    except Exception: pass

# ---------------------------------------------------------
# 4. 텔레그램 설정
# ---------------------------------------------------------
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# ---------------------------------------------------------
# 5. 키움 API 설정 (모의/실전 분기)
# ---------------------------------------------------------
# 공백 문자 제거를 위해 strip() 추가
if MOCK_TRADE:
    KIWOOM_REST_API_KEY = os.getenv("MOCK_KIWOOM_REST_API_KEY", "").strip()
    KIWOOM_SECRET = os.getenv("MOCK_KIWOOM_SECRET", "").strip()
    KIWOOM_HOST_URL = os.getenv("MOCK_KIWOOM_HOST_URL", "").strip()
    KIWOOM_SOCKET_URL = os.getenv("MOCK_KIWOOM_SOCKET_URL", "").strip()
    KIWOOM_ACCOUNT_NO = os.getenv("MOCK_KIWOOM_ACCOUNT_NO", "").strip()
    MODE_MSG = "🟢 모의투자 (Virtual)"
    KEY_PREFIX = "MOCK_"
else:
    KIWOOM_REST_API_KEY = os.getenv("REAL_KIWOOM_REST_API_KEY", "").strip()
    KIWOOM_SECRET = os.getenv("REAL_KIWOOM_SECRET", "").strip()
    KIWOOM_HOST_URL = os.getenv("REAL_KIWOOM_HOST_URL", "").strip()
    KIWOOM_SOCKET_URL = os.getenv("REAL_KIWOOM_SOCKET_URL", "").strip()
    KIWOOM_ACCOUNT_NO = os.getenv("REAL_KIWOOM_ACCOUNT_NO", "").strip()
    MODE_MSG = "🔴 실전투자 (REAL)"
    KEY_PREFIX = "REAL_"

# ---------------------------------------------------------
# 6. 설정 상태 진단 및 출력 (중요)
# ---------------------------------------------------------
print(f"[Config] ⚙️  투자 모드: {MODE_MSG} | 계좌: {KIWOOM_ACCOUNT_NO}")

# API 키 상태 마스킹 출력
masked_key = (KIWOOM_REST_API_KEY[:5] + "..." + KIWOOM_REST_API_KEY[-3:]) if len(KIWOOM_REST_API_KEY) > 10 else "❌ 없음"
masked_secret = "✅ 설정됨" if len(KIWOOM_SECRET) > 5 else "❌ 없음"

print(f"[Config] 🔑 App Key: {masked_key}")
print(f"[Config] 🔐 Secret : {masked_secret}")

# 필수 키 누락 시 강력한 경고
if not KIWOOM_REST_API_KEY or not KIWOOM_SECRET:
    print(f"\n{'='*50}")
    print(f"🚫 [치명적 오류] {KEY_PREFIX}KIWOOM_REST_API_KEY 또는 SECRET이 설정되지 않았습니다!")
    print(f"👉 .env 파일에 다음 변수가 있는지 확인하세요:")
    print(f"   - {KEY_PREFIX}KIWOOM_REST_API_KEY")
    print(f"   - {KEY_PREFIX}KIWOOM_SECRET")
    print(f"   - {KEY_PREFIX}KIWOOM_ACCOUNT_NO")
    print(f"{'='*50}\n")

if DEBUG_MODE:
    print("[Config] 🕵️  디버그 모드: ON")